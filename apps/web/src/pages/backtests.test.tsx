import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Backtests from './Backtests'
import type {
  BacktestListResponse,
  BacktestOut,
  BacktestSpecView,
  StoredStrategyView,
} from '@/api/types'

/**
 * What the backtests screen states, and what it refuses to.
 *
 * This is the only screen in the app that starts work, so the assertions divide
 * into two halves: what a run's four states look like, and what the form will and
 * will not offer.
 *
 * 1. **Four states, four renderings.** A queued run says it is waiting rather
 *    than showing an elapsed time from a timestamp nobody wrote; a running one
 *    shows a bar with counts; a failed one shows its reason; a done one shows its
 *    headline figures.
 * 2. **A caveat travels with the result.** A number a reader has already seen is a
 *    number they have already believed, so the server's warnings are rendered
 *    above the metrics, not below them.
 * 3. **A strategy no worker has run cannot be queued**, because
 *    `backtest_runs.strategy_id` is a foreign key onto a table a worker writes —
 *    and offering it would produce a 409 for a choice this screen invited.
 * 4. **A refusal is shown verbatim.** The API's 400 for missing history names the
 *    exact `backfill_bars.py` command; paraphrasing it would turn the one
 *    actionable message on this screen into a dead end.
 * 5. **Read-only sessions can read and compare, and cannot queue.** ADR 0009 —
 *    authorisation is about the act.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const SPEC: BacktestSpecView = {
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
}

const STRATEGY: StoredStrategyView = {
  id: 'sma_crossover',
  name: 'sma_crossover',
  description: 'a moving-average crossover',
  kind: 'coded',
  class_name: 'SmaCrossover',
  params: {},
  ruleset: null,
  state: 'active',
  universe: ['SPY'],
  timeframe: '1d',
  risk_config: {},
  created_at: '2026-03-02T14:30:00Z',
  last_started_at: '2026-08-19T13:30:00Z',
}

const DONE_METRICS = {
  total_return: 0.184,
  cagr: 0.176,
  sharpe: 1.21,
  sortino: 1.6,
  calmar: 0.9,
  volatility: 0.14,
  max_drawdown: -0.2,
  max_drawdown_duration_days: 41,
  num_trades: 120,
  win_rate: 0.52,
  // Legitimately unavailable: an infinite profit factor is stored as null,
  // because it is not legal JSON and it means too few trades rather than
  // perfection.
  profit_factor: null,
  expectancy: 42.5,
  avg_win: 310.2,
  avg_loss: -180.4,
  largest_win: 1200.0,
  largest_loss: -640.0,
  exposure_pct: 0.61,
  turnover: 3.4,
}

function run(overrides: Partial<BacktestOut> = {}): BacktestOut {
  return {
    id: 'run-1',
    strategy_id: 'sma_crossover',
    status: 'done',
    spec: SPEC,
    metrics: DONE_METRICS,
    error: null,
    queued_at: '2026-08-20T09:00:00Z',
    started_at: '2026-08-20T09:00:05Z',
    finished_at: '2026-08-20T09:02:10Z',
    progress: null,
    warnings: [],
    ...overrides,
  }
}

function list(runs: BacktestOut[], limitReached = false): BacktestListResponse {
  return { runs, limit_reached: limitReached }
}

/**
 * Routes by path, because this screen reads three endpoints and posts to a
 * fourth. A single-response stub would have the strategies query and the runs
 * query reading each other's body.
 *
 * **Longest path wins.** `/backtests` is a prefix of `/backtests/run-1/trades`,
 * so a first-match rule would serve the run *list* to the trades query — which
 * is a silent wrong answer rather than an error, and exactly the shape of bug
 * this helper exists to avoid.
 */
function stubRoutes(
  routes: Record<string, { status?: number; body: unknown }>,
  fallback: { status?: number; body: unknown } = { body: {} },
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const key =
      Object.keys(routes)
        .filter((candidate) => {
          const separator = candidate.indexOf(' ')
          return (
            candidate.slice(0, separator) === method && url.includes(candidate.slice(separator + 1))
          )
        })
        .sort((a, b) => b.length - a.length)
        .at(0) ?? null
    const matched = (key === null ? undefined : routes[key]) ?? fallback
    const status = matched.status ?? 200
    return {
      ok: status < 400,
      status,
      statusText: 'stub',
      json: async () => matched.body,
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/**
 * Open a run's detail panel.
 *
 * By its window rather than its strategy name: the name also appears in the
 * form's picker and in the filter select, so `getByText` on it finds three
 * elements. The click lands anywhere in the row — the handler is on the `<tr>`.
 */
async function openRun(day = '2024-01-01') {
  fireEvent.click(await screen.findByText(day))
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Backtests />
    </QueryClientProvider>,
  )
}

/** The session and strategies reads every render of this page performs. */
function baseRoutes(scope: 'full' | 'read' = 'full') {
  return {
    'GET /auth/me': { body: { user: 'operator', scope } },
    'GET /strategies': {
      body: { strategies: [STRATEGY], available: [], never_run: [] },
    },
  }
}

describe('a run that is waiting', () => {
  it('says it is waiting rather than showing a start time it does not have', async () => {
    // `started_at` is genuinely null on a queued run — the whole reason the
    // column is nullable. An elapsed time here would be counted from a timestamp
    // nobody wrote.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ status: 'queued', started_at: null, finished_at: null, metrics: null })]),
      },
    })
    renderPage()

    expect(await screen.findByText(/waiting for a worker/)).toBeTruthy()
    expect(screen.getByText('not finished')).toBeTruthy()
  })
})

describe('a run in progress', () => {
  it('shows the bar counts, not just a percentage', async () => {
    // A percentage alone cannot tell a slow run from one whose range turned out
    // to hold forty bars.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([
          run({
            status: 'running',
            finished_at: null,
            metrics: null,
            progress: {
              bars_done: 125,
              bars_total: 500,
              fraction: 0.25,
              at: '2026-08-20T09:01:00Z',
            },
          }),
        ]),
      },
    })
    renderPage()

    expect(await screen.findByText(/125 \/ 500 bars/)).toBeTruthy()
  })

  it('says nothing is reported yet rather than showing zero progress', async () => {
    // A bar at 0% reads as stalled. "The job has not reported yet" is a
    // different fact and the honest one.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ status: 'running', finished_at: null, metrics: null, progress: null })]),
      },
    })
    renderPage()

    expect(await screen.findByText(/no progress reported yet/)).toBeTruthy()
  })
})

describe('a run that failed', () => {
  it('shows the reason on the row', async () => {
    // A run stuck at "running" forever is the worst outcome here; one that says
    // "failed" with no reason is the second worst.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([
          run({
            status: 'failed',
            metrics: null,
            error: 'No stored bars for QQQ. Backfill first: scripts/backfill_bars.py',
          }),
        ]),
      },
    })
    renderPage()

    expect(await screen.findByText(/No stored bars for QQQ/)).toBeTruthy()
  })
})

describe('a finished run', () => {
  it('shows its headline figures in the list', async () => {
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
    })
    renderPage()

    expect(await screen.findByText(/\+18\.40%/)).toBeTruthy()
    expect(screen.getByText(/1\.21/)).toBeTruthy()
  })

  it('flags a zero-cost run in the list, because it invalidates the row', async () => {
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ spec: { ...SPEC, cost_model: 'zero' } })]),
      },
    })
    renderPage()

    expect(await screen.findByText(/zero cost/)).toBeTruthy()
  })

  it('renders an unavailable metric as a dash, never as zero', async () => {
    // `profit_factor` is null when nothing lost money, which means too few
    // trades rather than a perfect strategy. Zero is a value a reader acts on.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    await openRun()

    const label = await screen.findByText('Profit factor')
    const value = label.parentElement?.querySelector('dd')
    expect(value?.textContent).toBe('—')
  })

  it('puts the caveats above the numbers', async () => {
    // A number a reader has already seen is a number they have already believed.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([
          run({
            warnings: ['only 4 trades — under about 30 the statistics above mean very little'],
          }),
        ]),
      },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    await openRun()

    const warning = await screen.findByText(/only 4 trades/)
    const metrics = await screen.findByText('Metrics')
    // `compareDocumentPosition` rather than a snapshot: the property is the
    // order, and asserting on markup would break on any restyle.
    expect(warning.compareDocumentPosition(metrics) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('labels the money-shaped metrics as statistics rather than as ledger figures', async () => {
    // `expectancy`, `avg_win` and the rest are computed in float space by
    // `compute_all`, so the precision is gone before serialisation. Formatting
    // them like a balance would claim precision the response does not carry.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    await openRun()

    expect(await screen.findByText(/float statistics over the return series/)).toBeTruthy()
  })
})

describe('the trades of a finished run', () => {
  it('shows why each position closed', async () => {
    // The reason the engine sets `Order.purpose`: without it every exit here
    // would read "signal", stop-outs included.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': {
        body: {
          run_id: 'run-1',
          trades: [
            {
              trade_id: 't1',
              symbol: 'SPY',
              side: 'long',
              entry_ts: '2024-02-01T14:30:00Z',
              exit_ts: '2024-02-08T14:30:00Z',
              entry_price: '100.25',
              exit_price: '96.10',
              qty: '100',
              net_pnl: '-415.00',
              fees: '2.00',
              holding_period_hours: 168,
              exit_reason: 'stop_loss',
            },
          ],
        },
      },
    })
    renderPage()

    await openRun()

    expect(await screen.findByText('stop_loss')).toBeTruthy()
    // Money straight from the string the server sent — no parseFloat anywhere
    // between the Decimal in the engine and this text.
    expect(screen.getByText(/-415\.00/)).toBeTruthy()
  })

  it('says an open position is not a round trip rather than reporting nothing', async () => {
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    await openRun()

    expect(await screen.findByText(/closed no round trips/)).toBeTruthy()
    expect(screen.getByText(/still open when the window ends/)).toBeTruthy()
  })
})

describe('the form', () => {
  it('offers only strategies a worker has run', async () => {
    // `backtest_runs.strategy_id` is a foreign key onto a table a worker writes,
    // so offering an unrun class would invite a 409.
    stubRoutes({
      ...baseRoutes(),
      'GET /strategies': {
        body: {
          strategies: [],
          available: [
            {
              name: 'sma_crossover',
              class_name: 'SmaCrossover',
              description: '',
              params_schema: {},
              has_run: false,
            },
          ],
          never_run: ['sma_crossover'],
        },
      },
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    expect(await screen.findByText(/No strategy can be backtested yet/)).toBeTruthy()
    // And it says what to do about it, rather than leaving a disabled form:
    // the class exists, so the fix is to give a worker the strategy once.
    // Awaited, not `getByText`: before the strategies query resolves the panel
    // renders its generic sentence, which contains the same word inside a longer
    // string. The `<code>` element only appears once the never-run list arrives.
    expect(await screen.findByText('WORKER_STRATEGY')).toBeTruthy()
    expect(screen.getByText('sma_crossover')).toBeTruthy()
  })

  it('shows the API refusal verbatim, including the backfill command', async () => {
    const detail =
      'No stored bars for SPY in the requested window. Backfill first: ' +
      'scripts/backfill_bars.py --symbols SPY --start 2024-01-01'
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { status: 400, body: { detail } },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    expect(await screen.findByText(/--symbols SPY --start 2024-01-01/)).toBeTruthy()
  })

  it('defaults to a fixed share count and sends no sizing value for it', async () => {
    // The server reads a missing `sizing_value` as "use qty", which is what
    // keeps a request that names neither field the run it has always been.
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.sizing_method).toBe('fixed_qty')
    expect(body.sizing_value).toBeNull()
    expect(body.qty).toBe('100')
  })

  it('swaps the input and clears the value when the sizing method changes', async () => {
    // 100 shares and 100x the account are the same three characters. Carrying
    // the number across would reinterpret it silently, and the server would
    // accept it — `equity_pct` of 100 is a valid fraction.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    await screen.findByPlaceholderText('SPY')
    expect(screen.getByLabelText('Shares per entry')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Sizing'), { target: { value: 'risk_pct' } })

    expect(screen.queryByLabelText('Shares per entry')).toBeNull()
    const value = screen.getByLabelText('Sizing value') as HTMLInputElement
    expect(value.value).toBe('')
    // The unit is stated, because the field changes meaning per method.
    expect(value.placeholder).toContain('fraction of equity at risk')
  })

  it('sends the chosen method and its value', async () => {
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.change(screen.getByLabelText('Sizing'), { target: { value: 'equity_pct' } })
    fireEvent.change(screen.getByLabelText('Sizing value'), { target: { value: '0.05' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.sizing_method).toBe('equity_pct')
    // A string, like every other decimal the form sends.
    expect(body.sizing_value).toBe('0.05')
  })

  it('sends money as a string and a timezone-aware window', async () => {
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'spy, qqq' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    // Awaited: `mutate` is asynchronous, so a synchronous assertion here would
    // check before the request had left.
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    // A string, because `<input type="number">` would have made it a float and
    // every figure the run reports would descend from that.
    expect(body.starting_cash).toBe('100000')
    expect(typeof body.starting_cash).toBe('string')
    // Upper-cased and trimmed here, and re-normalised by the server anyway.
    expect(body.symbols).toEqual(['SPY', 'QQQ'])
    // Explicitly UTC: a date input yields a bare day, and the server refuses a
    // naive datetime at the boundary rather than assuming a zone.
    expect(body.start.endsWith('Z')).toBe(true)
    expect(body.end.endsWith('Z')).toBe(true)
  })
})

describe('a read-only session', () => {
  it('cannot queue a run and is told why', async () => {
    stubRoutes({
      ...baseRoutes('read'),
      'GET /backtests': { body: list([run()]) },
    })
    renderPage()

    expect(await screen.findByText(/This session is read-only/)).toBeTruthy()
    expect(screen.getByText('Queue backtest')).toHaveProperty('disabled', true)
  })

  it('can still read the runs', async () => {
    stubRoutes({
      ...baseRoutes('read'),
      'GET /backtests': { body: list([run()]) },
    })
    renderPage()

    // Comparing performs no act, so it is a GET and a read-only session reaches
    // it (ADR 0009). The rows are readable either way.
    expect(await screen.findByText(/\+18\.40%/)).toBeTruthy()
  })
})

describe('comparison', () => {
  it('carries the overfitting warning and marks no winner', async () => {
    const second = run({ id: 'run-2', metrics: { ...DONE_METRICS, sharpe: 0.4 } })
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests/compare': {
        body: {
          runs: [run(), second],
          metrics: { sharpe: { 'run-1': 1.21, 'run-2': 0.4 } },
          overfitting_warning:
            'Comparing variants and picking the best is how overfitting happens.',
        },
      },
      'GET /backtests': { body: list([run(), second]) },
    })
    renderPage()

    fireEvent.click(await screen.findByLabelText('compare run-1'))
    fireEvent.click(screen.getByLabelText('compare run-2'))

    expect(await screen.findByText(/overfitting happens/)).toBeTruthy()
    // Stated in the UI, because the absence of a highlight is not self-evident.
    expect(screen.getByText(/No column is marked as the winner/)).toBeTruthy()
  })

  it('does not offer to compare a run with no result', async () => {
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ status: 'queued', metrics: null, started_at: null })]),
      },
    })
    renderPage()

    expect(await screen.findByLabelText('compare run-1')).toHaveProperty('disabled', true)
  })
})

describe('an empty list', () => {
  it('says nothing has been queued rather than showing an empty table', async () => {
    stubRoutes({ ...baseRoutes(), 'GET /backtests': { body: list([]) } })
    renderPage()

    expect(await screen.findByText(/No backtest has been queued yet/)).toBeTruthy()
  })

  it('says when older runs were not fetched', async () => {
    // A list that stops at exactly the limit looks identical to one that ended.
    stubRoutes({ ...baseRoutes(), 'GET /backtests': { body: list([run()], true) } })
    renderPage()

    expect(await screen.findByText(/older ones this page did not fetch/)).toBeTruthy()
  })
})
