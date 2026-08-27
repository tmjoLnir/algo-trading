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
 * 3. **Every strategy the platform has is offered**, whether a worker has run it
 *    or not. `backtest_runs.strategy_id` is a foreign key onto a table a worker
 *    writes, which used to mean this picker listed only what had already been
 *    through one — an accident of deployment, on the screen whose subject is
 *    comparing strategies. The API writes that row when it queues the first run,
 *    so the picker is the union of the stored rows and the registered classes.
 * 4. **A refusal is shown verbatim.** The API's 400 for missing history names the
 *    exact `backfill_bars.py` command; paraphrasing it would turn the one
 *    actionable message on this screen into a dead end.
 * 5. **Read-only sessions can read and compare, and cannot queue.** ADR 0009 —
 *    authorisation is about the act.
 * 6. **Any single run can be written to a file**, and the file says which run and
 *    what state it was in. What it must never do is claim a result the run does
 *    not have.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  // jsdom implements neither, so `captureDownloads` adds them. Removed rather
  // than left behind: a test that forgot to install them would otherwise pass
  // against another test's spy.
  delete (URL as Partial<typeof URL>).createObjectURL
  delete (URL as Partial<typeof URL>).revokeObjectURL
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
  stop_type: '',
  stop_value: '',
  stop_period: 14,
  stop_bars: 0,
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

/** Money as decimal strings and counts as integers, exactly as the server sends. */
const DONE_TOTALS = {
  starting_equity: '100000',
  ending_equity: '118400',
  total_return: '0.184',
  realized_pnl: '16400',
  unrealized_pnl: '2000',
  fees: '128.55',
  open_positions: 0,
  orders: 240,
  filled_orders: 236,
  signals: 251,
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
    totals: DONE_TOTALS,
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

/** The labels a `<select>` offers, in order. */
function optionsOf(select: HTMLElement) {
  return [...select.querySelectorAll('option')].map((option) => option.textContent)
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

/**
 * Catch the files the page hands to the browser.
 *
 * jsdom has no download machinery — no `URL.createObjectURL`, and an anchor
 * click that would navigate rather than save — so the seam is stubbed at both
 * ends: the blob is captured where it is created, and the name where it is
 * clicked. Asserting on the bytes rather than on a spy call is the point, since
 * what this feature promises is the *content* of a file.
 *
 * Read through a `FileReader` rather than `blob.text()`, which jsdom's `Blob`
 * does not implement.
 */
function captureDownloads() {
  const saved: { name: string; json: () => Promise<unknown> }[] = []
  let pending: Blob | null = null

  const readText = (blob: Blob) =>
    new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(reader.error)
      reader.readAsText(blob)
    })

  URL.createObjectURL = vi.fn((blob: Blob) => {
    pending = blob
    return 'blob:stub'
  })
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    const blob = pending
    saved.push({
      name: this.download,
      json: async () => JSON.parse(await readText(blob as Blob)) as unknown,
    })
  })

  return saved
}

/** The export button on a row. One per run, addressed by the run's id. */
function exportButton(runId = 'run-1') {
  return screen.findByLabelText(`download run ${runId} as JSON`)
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

describe("what a run's return is actually made of", () => {
  /**
   * The gap this closes: a run reported +202.8% on this screen with nothing to
   * say that *none of it was realised* — twenty positions still open at the
   * end, the whole return an unrealised mark. The metric set could not say so;
   * every per-trade statistic in it counts closed round trips, so a strategy
   * still holding everything has the same all-zero trade statistics as one that
   * never had an idea.
   */
  const holding = {
    ...DONE_TOTALS,
    total_return: '2.028',
    realized_pnl: '0',
    unrealized_pnl: '202800',
    ending_equity: '302800',
    open_positions: 20,
  }

  const openPanel = (totals: BacktestOut['totals']) => {
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run({ totals })]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()
    return openRun()
  }

  it('splits the return into what was banked and what is still a mark', async () => {
    await openPanel(DONE_TOTALS)

    const realised = await screen.findByText(/realised \(closed trades\)/)
    expect(realised.parentElement?.querySelector('dd')?.textContent).toBe('+16,400.00')
    const unrealised = await screen.findByText(/unrealised \(still open\)/)
    expect(unrealised.parentElement?.querySelector('dd')?.textContent).toBe('+2,000.00')
  })

  it('says how much of a return is a position rather than a track record', async () => {
    await openPanel(holding)

    const sentence = await screen.findByText(/20 positions still open at the end/)
    expect(sentence.textContent).toMatch(/202,800\.00 of unrealised mark-to-market/)
    expect(sentence.textContent).toMatch(/count closed round trips only/)
  })

  it('says nothing about open positions when the run closed everything', async () => {
    await openPanel({ ...DONE_TOTALS, unrealized_pnl: '0', open_positions: 0 })

    expect(await screen.findByText('Starting equity')).toBeTruthy()
    expect(screen.queryByText(/still open at the end/)).toBeNull()
  })

  it('puts the split above the statistics it changes the meaning of', async () => {
    await openPanel(holding)

    const sentence = await screen.findByText(/still open at the end/)
    const metrics = await screen.findByText('Metrics')
    expect(
      sentence.compareDocumentPosition(metrics) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('formats these as money, not as the float statistics of the same name', async () => {
    // `total_return` exists twice over: a decimal string here and a float in the
    // metric set. Both render as 18.40%, and they must reach the screen through
    // different formatters — `money.ts` takes only strings (CLAUDE.md §1.1).
    await openPanel(DONE_TOTALS)

    const fees = await screen.findByText('Fees and commissions')
    expect(fees.parentElement?.querySelector('dd')?.textContent).toBe('128.55')
  })

  it('says an old run has no split rather than showing it as zero', async () => {
    // A run stored before the server kept these computed them and threw them
    // away. Noughts here would be figures nobody can check.
    await openPanel(null)

    expect(await screen.findByText(/before the platform stored a run/)).toBeTruthy()
    expect(screen.queryByText(/still open at the end/)).toBeNull()
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
  it('trims the strategy id, because the server does', async () => {
    // A `strategies` row carries whatever `Strategy.name` the worker booted
    // with, so one can arrive padded. `POST /backtests` strips before both the
    // registry lookup and the spec it stores, so sending the raw value is
    // accepted at the door and then misses the foreign key onto
    // `strategies.id` — reported as a strategy no worker has ever run, which is
    // a sentence about the wrong thing.
    const padded = { ...STRATEGY, id: '  sma_crossover  ' }
    const fetchMock = stubRoutes({
      'GET /auth/me': { body: { user: 'operator', scope: 'full' } },
      'GET /strategies': { body: { strategies: [padded], available: [], never_run: [] } },
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
    expect(JSON.parse(String(post?.[1]?.body)).strategy_id).toBe('sma_crossover')
  })

  it('will not queue a strategy whose id is blank, and says why', async () => {
    // The shape of "the strategy is right there and the server says it is
    // empty": a row with a name to show and no id to send. The strategy is the
    // one field on this form nobody types — it is derived from a list fetched at
    // runtime — so it is the only one that can silently be empty. Posting it
    // would spend a round trip to be told `strategy_id is empty`, which is true
    // and unreadable next to a picker visibly showing a strategy.
    const blank = { ...STRATEGY, id: '', name: 'sma_crossover' }
    const fetchMock = stubRoutes({
      'GET /auth/me': { body: { user: 'operator', scope: 'full' } },
      'GET /strategies': { body: { strategies: [blank], available: [], never_run: [] } },
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })

    expect(await screen.findByText(/blank id, so there is nothing to queue/)).toBeTruthy()
    expect(screen.getByText('Queue backtest')).toHaveProperty('disabled', true)

    fireEvent.click(screen.getByText('Queue backtest'))
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(false)
  })

  it('treats a whitespace-only id as blank rather than sending it', async () => {
    // `'  '` is not an id. The server would strip it to `''` and refuse; the
    // difference is that here it is refused before the request, beside the
    // control that produced it.
    const whitespace = { ...STRATEGY, id: '   ' }
    const fetchMock = stubRoutes({
      'GET /auth/me': { body: { user: 'operator', scope: 'full' } },
      'GET /strategies': { body: { strategies: [whitespace], available: [], never_run: [] } },
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })

    // The disabled button is the assertion, not the absent POST: a POST is
    // fired asynchronously, so checking for its absence straight after a click
    // passes whether or not the guard exists.
    expect(await screen.findByText('Queue backtest')).toHaveProperty('disabled', true)
    expect(screen.getByText(/blank id, so there is nothing to queue/)).toBeTruthy()

    fireEvent.click(screen.getByText('Queue backtest'))
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(false),
    )
  })

  it('queues the strategy the picker is showing, without it being touched first', async () => {
    // The regression this exists for: the picker's initial value was read from
    // `runnable[0]` at *mount*, and this form mounts on the first render — before
    // the strategies query has resolved, when that list is still empty. The state
    // initialiser does not re-run when the data lands, so the select ended up
    // holding `''` while displaying a strategy, and the button posted an empty
    // `strategy_id`. The API answered `unknown strategy ''; registered:
    // ['sma_crossover']`, which reads as a registry fault and is a form fault.
    //
    // Deliberately never touches the Strategy select: choosing anything fires
    // `onChange` and repairs the state, which is why every other test here — each
    // of which asserts some *other* field of the same POST — passed throughout.
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
    expect(body.strategy_id).toBe('sma_crossover')
  })

  it('queues the strategy that was chosen, not the one it falls back to', async () => {
    // The other half of the fix, and the thing a naive repair breaks: the
    // fallback to the first runnable strategy must apply only while nothing
    // valid has been chosen. An explicit choice has to survive every later
    // render of this form — the strategies query refetches on window focus, so
    // a fallback that reasserted itself would silently re-point a queued run at
    // a strategy the person did not pick.
    const second: StoredStrategyView = { ...STRATEGY, id: 'donchian', name: 'donchian' }
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /strategies': {
        body: { strategies: [STRATEGY, second], available: [], never_run: [] },
      },
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 'donchian' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.strategy_id).toBe('donchian')
  })

  it('offers a registered class no worker has run, and queues it', async () => {
    // The defect this replaces: the picker was built from the `strategies`
    // table alone, so it listed whichever strategies had happened through a
    // *trading* worker or the seed script — usually one — on the screen whose
    // whole subject is comparing strategies. `POST /backtests` writes the row a
    // run needs when it queues the first one, so a class the code registers is
    // offered and runnable before anything has ever loaded it.
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /strategies': {
        body: {
          strategies: [],
          available: [
            {
              name: 'buy_and_hold',
              class_name: 'BuyAndHold',
              description: '',
              params_schema: {},
              has_run: false,
            },
          ],
          never_run: ['buy_and_hold'],
        },
      },
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    const picker = await screen.findByLabelText('Strategy')
    expect(optionsOf(picker)).toEqual(['buy_and_hold'])
    // Said beside the control rather than in place of it: that a worker has
    // never loaded this strategy is worth knowing before reading a result, and
    // is not a reason it cannot be run.
    expect(screen.getByText(/BuyAndHold · no worker has run this yet/)).toBeTruthy()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    expect(JSON.parse(String(post?.[1]?.body)).strategy_id).toBe('buy_and_hold')
  })

  it('names a strategy once when it is in both halves of the response', async () => {
    // A stored row and a registered class are the same strategy seen from two
    // sides — `available[].has_run` is exactly that join — so a union that
    // offered both would ask somebody to choose between one strategy and
    // itself. The stored row wins: it carries the id a run must point at.
    stubRoutes({
      ...baseRoutes(),
      'GET /strategies': {
        body: {
          strategies: [STRATEGY],
          available: [
            {
              name: 'sma_crossover',
              class_name: 'SmaCrossover',
              description: '',
              params_schema: {},
              has_run: true,
            },
            {
              name: 'buy_and_hold',
              class_name: 'BuyAndHold',
              description: '',
              params_schema: {},
              has_run: false,
            },
          ],
          never_run: ['buy_and_hold'],
        },
      },
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    expect(optionsOf(await screen.findByLabelText('Strategy'))).toEqual([
      'buy_and_hold',
      'sma_crossover',
    ])
    // And the filter over the runs reads the same list, so a strategy queued
    // from the form can be filtered to afterwards.
    expect(optionsOf(screen.getByLabelText('Filter by strategy'))).toEqual([
      'Every strategy',
      'buy_and_hold',
      'sma_crossover',
    ])
  })

  it('says there is nothing to run only when both halves are empty', async () => {
    // Which is now a different sentence from "nothing has been run": a
    // registered class needs no row to be offered, so an empty picker means the
    // code registers nothing *and* the table holds nothing.
    stubRoutes({
      ...baseRoutes(),
      'GET /strategies': { body: { strategies: [], available: [], never_run: [] } },
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    expect(await screen.findByText(/No strategy to backtest/)).toBeTruthy()
    expect(screen.getByText(/every strategy class the code registers/)).toBeTruthy()
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

  it('asks for no stop by default, so an unconfigured request is unchanged', async () => {
    // Every run this platform has stored was unprotected beyond what its
    // strategy emitted. Defaulting the form to `atr` would change what a
    // re-queued old spec reports, which is why the empty option is offered
    // rather than removed.
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    // No stop chosen, so there is nothing to give a value to.
    expect(screen.queryByLabelText('Stop value')).toBeNull()
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.stop_type).toBe('')
    expect(body.stop_value).toBeNull()
    expect(body.stop_bars).toBe(0)
  })

  it('reveals the value field and states what the number means', async () => {
    // A multiple of ATR and a fraction of price are both '2' to a text input,
    // and the two put the stop in completely different places.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    await screen.findByPlaceholderText('SPY')
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'atr' } })

    const value = screen.getByLabelText('Stop value') as HTMLInputElement
    expect(value.placeholder).toContain('multiple of ATR')
    // An ATR stop measures its multiple against a lookback, so that is asked
    // for too — and only for the types that read one.
    expect(screen.getByLabelText('ATR period')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'fixed_pct' } })
    expect(screen.queryByLabelText('ATR period')).toBeNull()
    expect((screen.getByLabelText('Stop value') as HTMLInputElement).placeholder).toContain(
      'fraction',
    )
  })

  it('clears the value when the stop type changes', async () => {
    // Same hazard as the sizing value: 2 carried from an ATR multiple into a
    // fixed percent is a 200% stop, which the server would accept as a number
    // and refuse only as a level below zero.
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
    })
    renderPage()

    await screen.findByPlaceholderText('SPY')
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'atr' } })
    fireEvent.change(screen.getByLabelText('Stop value'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'fixed_pct' } })

    expect((screen.getByLabelText('Stop value') as HTMLInputElement).value).toBe('')
  })

  it('sends a time stop as a bar count rather than a price distance', async () => {
    // A time stop says when to leave, not where. Sending its number as
    // `stop_value` would be a distance the server has nowhere to measure, and
    // it refuses a time stop with no bar count rather than defaulting one.
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'time' } })
    expect((screen.getByLabelText('Stop value') as HTMLInputElement).placeholder).toContain('bars')
    fireEvent.change(screen.getByLabelText('Stop value'), { target: { value: '10' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.stop_type).toBe('time')
    expect(body.stop_bars).toBe(10)
    expect(body.stop_value).toBeNull()
  })

  it('sends a price stop as a string, with its ATR period', async () => {
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([]) },
      'POST /backtests': { body: run({ status: 'queued' }) },
    })
    renderPage()

    fireEvent.change(await screen.findByPlaceholderText('SPY'), { target: { value: 'SPY' } })
    fireEvent.change(screen.getByLabelText('Stop'), { target: { value: 'atr' } })
    fireEvent.change(screen.getByLabelText('Stop value'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('ATR period'), { target: { value: '20' } })
    fireEvent.click(screen.getByText('Queue backtest'))

    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    )
    const post = fetchMock.mock.calls.find((call) => call[1]?.method === 'POST')
    const body = JSON.parse(String(post?.[1]?.body))
    expect(body.stop_type).toBe('atr')
    // A string, because the multiple becomes a price distance on the server and
    // a float would carry binary rounding into it.
    expect(body.stop_value).toBe('2')
    expect(body.stop_period).toBe(20)
    expect(body.stop_bars).toBe(0)
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

describe('exporting a run', () => {
  it('writes the run, its curve and its trades to a file named for it', async () => {
    // The whole result in one file: what was asked for, what came back, the
    // curve and every trade. A reader who keeps this can answer
    // docs/BACKTESTING.md's checklist a year later without the platform.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': {
        body: { run_id: 'run-1', points: [['2024-01-02T00:00:00Z', '100000.00']] },
      },
      'GET /backtests/run-1/trades': {
        body: {
          run_id: 'run-1',
          trades: [{ symbol: 'SPY', entry_price: '450.125', net_pnl: '-0.006', qty: '100' }],
        },
      },
    })
    renderPage()

    fireEvent.click(await exportButton())

    await waitFor(() => expect(saved.length).toBe(1))
    expect(saved[0]?.name).toBe('backtest-sma_crossover-2026-08-20-run-1.json')

    const file = (await saved[0]?.json()) as {
      run: BacktestOut
      equity_curve: string[][]
      trades: { net_pnl: string }[]
    }
    expect(file.run.id).toBe('run-1')
    expect(file.run.spec.timeframe).toBe('1d')
    expect(file.run.metrics?.sharpe).toBe(1.21)
    expect(file.equity_curve).toEqual([['2024-01-02T00:00:00Z', '100000.00']])
    expect(file.trades[0]?.net_pnl).toBe('-0.006')
  })

  it('keeps every monetary figure a string', async () => {
    // Rule §1.1 at the last place it can be broken. A price that went through a
    // double on the way to disk is a wrong number in a file somebody trusts —
    // and unlike a screen, a file has nothing beside it to check against.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ spec: { ...SPEC, starting_cash: '100000.333333333333333333' } })]),
      },
      'GET /backtests/run-1/equity-curve': {
        body: { run_id: 'run-1', points: [['2024-01-02T00:00:00Z', '100000.333333333333333333']] },
      },
      'GET /backtests/run-1/trades': {
        body: { run_id: 'run-1', trades: [{ entry_price: '450.125' }] },
      },
    })
    renderPage()

    fireEvent.click(await exportButton())

    await waitFor(() => expect(saved.length).toBe(1))
    const file = (await saved[0]?.json()) as {
      run: BacktestOut
      equity_curve: string[][]
      trades: { entry_price: string }[]
    }
    // Beyond a double's 15-17 significant digits: these digits cannot survive a
    // parse, so their presence is the proof that nothing parsed them.
    expect(file.run.spec.starting_cash).toBe('100000.333333333333333333')
    expect(file.equity_curve[0]?.[1]).toBe('100000.333333333333333333')
    expect(typeof file.trades[0]?.entry_price).toBe('string')
  })

  it('does not ask for a result a run has not produced', async () => {
    // The two endpoints answer a queued run with an empty list rather than a
    // 404, so asking would spend two requests to learn what the status already
    // says — and would then record `[]`, claiming an empty result where there is
    // none.
    const saved = captureDownloads()
    const fetchMock = stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ status: 'queued', started_at: null, finished_at: null, metrics: null })]),
      },
    })
    renderPage()

    fireEvent.click(await exportButton())

    await waitFor(() => expect(saved.length).toBe(1))
    const urls = fetchMock.mock.calls.map(([input]) => String(input))
    expect(urls.some((url) => url.includes('/equity-curve'))).toBe(false)
    expect(urls.some((url) => url.includes('/trades'))).toBe(false)

    const file = (await saved[0]?.json()) as { equity_curve: null; trades: null; run: BacktestOut }
    expect(file.equity_curve).toBeNull()
    expect(file.trades).toBeNull()
    // Still worth keeping: it is the record of exactly what was asked for.
    expect(file.run.status).toBe('queued')
    expect(file.run.spec.symbols).toEqual(['SPY'])
  })

  it('exports a failed run as a failure rather than as an empty result', async () => {
    // `RunRepository.fail` clears the curve and the trades — a partial curve
    // under a failed status is a chart of two of the five years somebody asked
    // about. The file has to say that, not show it as a run that traded nothing.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': {
        body: list([run({ status: 'failed', error: 'no bars for SPY', metrics: null })]),
      },
    })
    renderPage()

    fireEvent.click(await exportButton())

    await waitFor(() => expect(saved.length).toBe(1))
    const file = (await saved[0]?.json()) as { trades: null; run: BacktestOut }
    expect(file.run.error).toBe('no bars for SPY')
    expect(file.trades).toBeNull()
  })

  it('confirms on the row that it wrote a file', async () => {
    // A browser saving straight to a downloads folder shows nothing at all, and
    // a button that answers a click with silence reads as broken. The name is
    // the title, because it is what finds the file.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    fireEvent.click(await exportButton())

    const note = await screen.findByText('saved')
    expect(note.getAttribute('title')).toBe('backtest-sma_crossover-2026-08-20-run-1.json')
    expect(saved[0]?.name).toBe('backtest-sma_crossover-2026-08-20-run-1.json')
  })

  it('says so on the row when it could not export, and writes nothing', async () => {
    // A failure path, because a button that silently does nothing is the worst
    // outcome here: the reader believes they have the file.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': {
        status: 503,
        body: { detail: 'the database is unreachable' },
      },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    fireEvent.click(await exportButton())

    expect(await screen.findByText('could not export')).toBeTruthy()
    expect(saved.length).toBe(0)
  })

  it('exports one run without opening it or touching the others', async () => {
    // The click must not bubble to the row: a detail panel springing open on
    // every download would scroll the list out from under the next one. And the
    // failure above belongs to its own row — this asserts the other button is
    // still usable.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run(), run({ id: 'run-2' })]) },
      'GET /backtests/run-2/equity-curve': { body: { run_id: 'run-2', points: [] } },
      'GET /backtests/run-2/trades': { body: { run_id: 'run-2', trades: [] } },
    })
    renderPage()

    fireEvent.click(await exportButton('run-2'))

    await waitFor(() => expect(saved.length).toBe(1))
    expect(saved[0]?.name).toBe('backtest-sma_crossover-2026-08-20-run-2.json')
    // The detail panel renders this heading; the row click that would open it
    // never happened.
    expect(screen.queryByText('Metrics')).toBeNull()
  })

  it('lets a read-only session export', async () => {
    // Reading a result and writing it to disk performs no act, so unlike the
    // queue button this one is not gated on scope (ADR 0009).
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes('read'),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    const button = await exportButton()
    expect(button).toHaveProperty('disabled', false)
    fireEvent.click(button)

    await waitFor(() => expect(saved.length).toBe(1))
  })

  it('records a finished run that took no trades as an empty result, not a missing one', async () => {
    // `[]` and `null` are different facts and this is the one that is `[]`: the
    // run stored a result and it is empty, which is what a strategy that never
    // exited looks like.
    const saved = captureDownloads()
    stubRoutes({
      ...baseRoutes(),
      'GET /backtests': { body: list([run()]) },
      'GET /backtests/run-1/equity-curve': { body: { run_id: 'run-1', points: [] } },
      'GET /backtests/run-1/trades': { body: { run_id: 'run-1', trades: [] } },
    })
    renderPage()

    fireEvent.click(await exportButton())

    await waitFor(() => expect(saved.length).toBe(1))
    const file = (await saved[0]?.json()) as { equity_curve: string[][]; trades: unknown[] }
    expect(file.trades).toEqual([])
    expect(file.equity_curve).toEqual([])
  })
})
