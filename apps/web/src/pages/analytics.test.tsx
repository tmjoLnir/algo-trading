import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Analytics from './Analytics'
import PerformancePanel from '@/components/PerformancePanel'
import AttributionTable from '@/components/AttributionTable'
import TradesTable from '@/components/TradesTable'
import type {
  AttributionResponse,
  BacktestOut,
  LiveVsBacktestResponse,
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

    // The three window panels, not every request the page makes: the run picker
    // for the live-vs-backtest panel reads `/backtests`, which carries no
    // window and must not be counted as one that lost it. Selecting by path
    // rather than by count is also what keeps this assertion about the rule —
    // a fourth *windowed* read would still fail it.
    const urls = fetchMock.mock.calls
      .map(([input]) => String(input))
      .filter((url) => url.includes('/analytics/'))
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

/**
 * The live-vs-backtest panel, and the four things it must not do.
 *
 * This is the report docs/ANALYTICS.md calls the most important one here and
 * the one most easily read into saying something it does not, so every
 * assertion below is one of the ways it could quietly say the wrong thing:
 *
 * 1. **A null divergence is a dash.** Zero is the strongest claim this report
 *    can make — live matched the backtest exactly — and an absent value must
 *    never render as one. Absences are routine, not exotic: a stored run nulls
 *    its non-finite metrics, and an infinite `profit_factor` is precisely the
 *    run somebody holds a live record up against.
 * 2. **Every row carries its comparability.** Nine of the nineteen metrics are
 *    annualised or window-scaled, so a bare subtraction on those rows is
 *    measurement dressed as performance.
 * 3. **Nothing is chosen for the reader.** Which backtest a live record is
 *    judged against is the substance of the comparison; defaulting to the
 *    newest run would compare against a backtest nobody approved anything with.
 * 4. **The page's date range does not reach it.** The endpoint's live window is
 *    open at the start on purpose — the denominator for "has this held up" is
 *    the whole live record.
 */

const COMPARISON_RUN: BacktestOut = {
  id: 'run-1',
  strategy_id: 'sma_crossover',
  status: 'done',
  spec: {
    strategy_id: 'sma_crossover',
    symbols: ['SPY'],
    start: '2024-01-01T00:00:00Z',
    end: '2025-01-01T00:00:00Z',
    timeframe: '1d',
    starting_cash: '100000',
    cost_model: 'alpaca_equities',
    params: {},
    qty: '100',
    sizing_method: 'fixed_qty',
    sizing_value: '100',
  },
  metrics: { ...METRICS, profit_factor: null },
  error: null,
  queued_at: '2026-08-20T09:00:00Z',
  started_at: '2026-08-20T09:00:05Z',
  finished_at: '2026-08-20T09:02:10Z',
  progress: null,
  warnings: [],
}

const COMPARISON: LiveVsBacktestResponse = {
  live: {
    strategy_id: 'sma_crossover',
    metrics: { ...METRICS, avg_holding_period_hours: 18 },
    window: { start: '2026-07-01T14:30:00Z', end: '2026-08-19T20:00:00Z', days: 49.2 },
    requested_start: null,
    requested_end: '2026-08-21T00:00:00Z',
    num_trades: 5,
    symbols: ['SPY'],
    equity_points: 6,
    periods_per_year: 20,
  },
  backtest: {
    run_id: 'run-1',
    status: 'done',
    metrics: { ...METRICS, profit_factor: null, avg_holding_period_hours: 30.5 },
    window: { start: '2024-01-01T00:00:00Z', end: '2025-01-01T00:00:00Z', days: 366 },
    symbols: ['SPY'],
    timeframe: '1d',
    cost_model: 'alpaca_equities',
    qty: '100',
    starting_cash: '100000',
    finished_at: '2026-08-20T09:02:10Z',
    periods_per_year: 252,
    warnings: [],
  },
  // `profit_factor` is null on the backtest side, so its divergence is too.
  divergence: {
    total_return: 0.02,
    sharpe: -0.4,
    win_rate: -0.05,
    profit_factor: null,
    num_trades: -115,
    avg_holding_period_hours: -12.5,
  },
  comparability: {
    total_return: 'window',
    sharpe: 'annualised',
    win_rate: 'per_trade',
    profit_factor: 'per_trade',
    num_trades: 'window',
    avg_holding_period_hours: 'per_trade',
  },
  warnings: ['only 5 live round trips — under about 30 the statistics mean very little'],
}

/** The three window panels answered, so only the comparison is under test. */
function comparisonRoutes(over: (url: string) => { status: number; body: unknown } | null) {
  return (url: string) => {
    const custom = over(url)
    if (custom) return custom
    if (url.includes('/backtests')) return { status: 200, body: { runs: [COMPARISON_RUN] } }
    if (url.includes('/performance')) return { status: 200, body: PERFORMANCE }
    if (url.includes('/attribution')) return { status: 200, body: ATTRIBUTION }
    return { status: 200, body: tradesResponse([TRADE]) }
  }
}

describe('live vs backtest', () => {
  it('asks for nothing until a run is named', async () => {
    const fetchMock = stubRoutes(comparisonRoutes(() => null))
    renderPage()
    await screen.findByText('AAPL')

    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(urls.some((url) => url.includes('live-vs-backtest'))).toBe(false)
    // And says so, because an empty panel that had silently picked a run would
    // look identical to one waiting to be told which.
    expect(screen.getByText(/defaulting to the newest run/)).toBeTruthy()
  })

  it('offers only completed runs', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('/backtests')
          ? {
              status: 200,
              body: {
                runs: [
                  COMPARISON_RUN,
                  { ...COMPARISON_RUN, id: 'run-2', status: 'queued', metrics: null },
                ],
              },
            }
          : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')

    const picker = screen.getByLabelText('Backtest run') as HTMLSelectElement
    const values = Array.from(picker.options).map((option) => option.value)
    // A queued run has no metrics; the endpoint answers 400 for one, so the
    // picker must not offer a choice the server will refuse.
    expect(values).toContain('run-1')
    expect(values).not.toContain('run-2')
  })

  it('renders an absent divergence as a dash and never as zero', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    const row = (await screen.findByText('profit_factor')).closest('tr') as HTMLTableRowElement
    const cells = within(row).getAllByRole('cell')
    // Live has a profit factor, the backtest's is null, so the divergence is
    // unavailable — and the row must say so rather than claiming they matched.
    expect(cells[3]?.textContent).toBe('—')
    expect(cells[3]?.textContent).not.toBe('0.00')
  })

  it('states the comparability of every row', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    const sharpeRow = (await screen.findByText('sharpe')).closest('tr') as HTMLTableRowElement
    expect(within(sharpeRow).getByText('annualised')).toBeTruthy()
    const winRateRow = screen.getByText('win_rate').closest('tr') as HTMLTableRowElement
    expect(within(winRateRow).getByText('per trade')).toBeTruthy()
  })

  it('carries the server warnings above the numbers', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    expect(await screen.findByText(/only 5 live round trips/)).toBeTruthy()
    // And marks no row good or bad, for the reason `BacktestComparison` marks
    // no winner: on most rows the sign is a difference, not a verdict.
    expect(screen.getByText(/no row is marked/)).toBeTruthy()
  })

  it('sends no window, so the comparison covers the whole live record', async () => {
    const fetchMock = stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })
    await screen.findByText('sharpe')

    const url = fetchMock.mock.calls
      .map(([input]) => String(input))
      .find((candidate) => candidate.includes('live-vs-backtest')) as string
    // Forwarding the page's 30-day range would compare a month of live against
    // a five-year backtest while looking like it had compared everything.
    expect(url).not.toContain('start=')
    expect(url).not.toContain('end=')
  })

  it('pins both sides to the backtest basis on request', async () => {
    const fetchMock = stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    // The basis comes from the server's own answer for this run, not from a
    // second copy of `periods_per_year_for` on the client.
    fireEvent.click(await screen.findByRole('checkbox', { name: /Pin both sides/ }))

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map(([input]) => String(input))
      expect(urls.some((url) => url.includes('periods_per_year=252'))).toBe(true)
    })
  })

  it('keeps the other panels when the comparison fails', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest')
          ? { status: 400, body: { detail: "run 'run-1' is 'failed' and has no metrics" } }
          : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    expect(await screen.findByText(/has no metrics/)).toBeTruthy()
    expect(screen.getByText('AAPL')).toBeTruthy()
  })

  it('orders rows by comparability, which is the stable key order', async () => {
    // `divergence` is built from a set union server-side, so its key order is
    // arbitrary and need not survive a restart. A table whose rows reshuffled
    // between two reads of the same run would be unusable.
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest')
          ? {
              status: 200,
              body: {
                ...COMPARISON,
                // Deliberately the reverse of `comparability`'s order.
                divergence: Object.fromEntries(
                  Object.entries(COMPARISON.divergence).reverse(),
                ) as LiveVsBacktestResponse['divergence'],
              },
            }
          : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    // Scoped to this panel's own table: the page renders three.
    const table = (await screen.findByText('sharpe')).closest('table') as HTMLTableElement
    const names = within(table)
      .getAllByRole('row')
      .slice(1)
      .map((row) => within(row).getAllByRole('cell')[0]?.textContent)
    expect(names).toEqual(Object.keys(COMPARISON.comparability))
  })

  it('renders a metric the basis map does not classify rather than dropping it', async () => {
    // A run stored before `METRIC_BASIS` grew a field. The row whose meaning
    // nobody has decided yet is the last one to hide.
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest')
          ? {
              status: 200,
              body: {
                ...COMPARISON,
                divergence: { ...COMPARISON.divergence, some_new_metric: 1.5 },
              },
            }
          : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    expect(await screen.findByText('some_new_metric')).toBeTruthy()
  })

  it('keeps a duration divergence in one unit', async () => {
    stubRoutes(
      comparisonRoutes((url) =>
        url.includes('live-vs-backtest') ? { status: 200, body: COMPARISON } : null,
      ),
    )
    renderPage()
    await screen.findByText('AAPL')
    fireEvent.change(screen.getByLabelText('Backtest run'), { target: { value: 'run-1' } })

    const row = (await screen.findByText('avg_holding_period_hours')).closest(
      'tr',
    ) as HTMLTableRowElement
    const cells = within(row).getAllByRole('cell')
    // `formatDuration` buckets by magnitude, so -12.5 hours would read as
    // `-750m` — the same quantity, unreadable beside a column of hours.
    expect(cells[3]?.textContent).toBe('-12.5h')
  })
})
