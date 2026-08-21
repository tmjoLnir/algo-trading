import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Strategies from './Strategies'
import type { AvailableStrategyView, StoredStrategyView, StrategiesResponse } from '@/api/types'

/**
 * What the strategies screen states, and what it refuses to.
 *
 * The screen exists for the gap between two lists: the strategy classes the
 * code registers, and the rows a worker has actually written. Every assertion
 * here is about that gap or about the two column names that mean less than they
 * say.
 *
 * 1. **A class nothing has ever run is called out**, because with
 *    `WORKER_STRATEGY` empty by default that is the ordinary state of a fresh
 *    install and no other screen could say so.
 * 2. **An empty table is not an empty platform.** "No worker has registered a
 *    strategy" and "there are no strategies" are different sentences.
 * 3. **`state` is shown as configured, not as running.** `ensure` writes
 *    `draft` once and never revisits it.
 * 4. **`updated_at` is rendered as what it records** — a worker started this —
 *    rather than as a suggestion somebody edited it.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const STORED: StoredStrategyView = {
  id: 'sma_crossover',
  name: 'sma_crossover',
  description: 'a moving-average crossover',
  kind: 'coded',
  class_name: 'SmaCrossover',
  params: { fast: 10, slow: 30 },
  ruleset: null,
  state: 'draft',
  universe: ['SPY', 'QQQ'],
  timeframe: '1d',
  risk_config: { max_position_pct: '0.1' },
  created_at: '2026-03-02T14:30:00Z',
  last_started_at: '2026-08-19T13:30:00Z',
}

const AVAILABLE: AvailableStrategyView = {
  name: 'sma_crossover',
  class_name: 'SmaCrossover',
  description: 'a moving-average crossover',
  params_schema: {},
  has_run: true,
}

function response(overrides: Partial<StrategiesResponse> = {}): StrategiesResponse {
  return {
    strategies: [STORED],
    available: [AVAILABLE],
    never_run: [],
    ...overrides,
  }
}

/**
 * An empty but valid `/risk/status`, for the panel this page now carries.
 *
 * Routed separately rather than letting the strategies body answer every
 * request. These cases are about the strategy lists, and a panel rendering
 * "the limits could not be read at all" underneath them would be a second
 * screen silently in a failure state inside tests that are not about it —
 * `components/risklimits.test.tsx` is where that panel is actually held.
 */
const RISK_STATUS = {
  as_of: '2026-08-20T14:30:00Z',
  book_as_of: null,
  book_age_seconds: null,
  book_published: false,
  equity: null,
  limits: [],
  unmarked_symbols: [],
}

/** An empty but valid `/risk/rejections`, for the other panel this page carries. */
const RISK_REJECTIONS = {
  rejections: [],
  by_rule: {},
  blind_spots: ['a stop exit refused by the risk chain is written to the log only'],
}

function stub(status: number, body: unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input)
    const risk = url.includes('/risk/')
    const riskBody = url.includes('/risk/rejections') ? RISK_REJECTIONS : RISK_STATUS
    return {
      ok: risk ? true : status < 400,
      status: risk ? 200 : status,
      statusText: 'stub',
      json: async () => (risk ? riskBody : body),
    } as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <Strategies />
    </QueryClientProvider>,
  )
}

describe('a strategy nothing has run', () => {
  it('is called out at the top of the screen', async () => {
    // The answer to "I wrote a strategy and nothing is happening", which no
    // other screen in this UI could give.
    stub(
      200,
      response({
        strategies: [],
        available: [{ ...AVAILABLE, has_run: false }],
        never_run: ['sma_crossover'],
      }),
    )
    renderPage()

    expect(await screen.findByText(/never been run by a worker/)).toBeTruthy()
  })

  it('says the default posture is expected rather than broken', async () => {
    stub(200, response({ strategies: [], never_run: ['sma_crossover'] }))
    renderPage()

    expect(await screen.findByText(/on a fresh install this is expected/)).toBeTruthy()
  })

  it('does not warn when a worker has run everything the code registers', async () => {
    stub(200, response())
    renderPage()

    await screen.findByText('Strategies a worker has run')
    expect(screen.queryByText(/never been run by a worker/)).toBeNull()
  })

  it('marks the class itself as never run in the registry table', async () => {
    stub(
      200,
      response({
        strategies: [],
        available: [{ ...AVAILABLE, has_run: false }],
        never_run: ['sma_crossover'],
      }),
    )
    renderPage()

    expect(await screen.findByText('never run')).toBeTruthy()
  })
})

describe('an empty table', () => {
  it('says nothing has run, not that nothing exists', async () => {
    // The registry panel below still lists the class, so the screen has to
    // distinguish the two rather than letting one empty list imply the other.
    stub(200, response({ strategies: [], never_run: ['sma_crossover'] }))
    renderPage()

    expect(await screen.findByText(/No worker has registered a strategy yet/)).toBeTruthy()
    expect(
      screen.getByText(/an empty one means nothing has run, not that nothing exists/),
    ).toBeTruthy()
  })
})

describe('the two misleading columns', () => {
  it('labels the state as configured rather than as running', async () => {
    // `ensure` writes `draft` once and never revisits it, so a strategy a
    // worker has been running for a month still reads draft.
    stub(200, response())
    renderPage()

    expect(await screen.findByText('as configured')).toBeTruthy()
  })

  it('renders a state it does not recognise rather than dropping the row', async () => {
    // `StoredStrategyView.state` is a bare string on the wire on purpose: a
    // database that has not run the e2b6d1a70f93 migration still holds
    // `active`, and a newer server may send a rung this build has never heard
    // of. The word is what a reader needs; only the tint is unknown.
    stub(200, response({ strategies: [{ ...STORED, state: 'active' }] }))
    renderPage()

    expect(await screen.findByText('active')).toBeTruthy()
  })

  it('renders updated_at as when a worker last started the strategy', async () => {
    // Serving it as "updated" would invite the reader to conclude somebody
    // edited it this morning.
    stub(200, response())
    renderPage()

    expect(await screen.findByText('A worker last started this')).toBeTruthy()
  })
})

describe('the stored rows', () => {
  it('shows the universe, parameters and risk config', async () => {
    stub(200, response())
    renderPage()

    // Located via the universe rather than the name: the name deliberately
    // appears in both tables, which is the whole point of the screen.
    const row = (await screen.findByText('SPY, QQQ')).closest('tr')
    expect(row).toBeTruthy()
    expect(within(row as HTMLElement).getByText('sma_crossover')).toBeTruthy()
    expect(within(row as HTMLElement).getByText(/fast=/)).toBeTruthy()
    expect(within(row as HTMLElement).getByText(/max_position_pct=/)).toBeTruthy()
  })

  it('renders a declarative strategy with its ruleset', async () => {
    // For a ruleset strategy the ruleset *is* the strategy, so omitting it
    // would make the screen useless for exactly those.
    stub(
      200,
      response({
        strategies: [
          {
            ...STORED,
            id: 'my_rules',
            name: 'my_rules',
            kind: 'ruleset',
            class_name: null,
            ruleset: { entry: ['rsi < 30'] },
          },
        ],
      }),
    )
    renderPage()

    expect(await screen.findByText(/declarative ruleset/)).toBeTruthy()
    expect(screen.getByText(/rsi < 30/)).toBeTruthy()
  })

  it('renders an empty parameter set as a dash rather than as blank', async () => {
    stub(200, response({ strategies: [{ ...STORED, params: {}, risk_config: {} }] }))
    renderPage()

    // `findAllByText`: the name is in the stored table and the registry table.
    await screen.findAllByText('sma_crossover')
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('the page', () => {
  it('asks for every state by default', async () => {
    const fetchMock = stub(200, response())
    renderPage()
    await screen.findByText('Strategies a worker has run')

    const url = String(fetchMock.mock.calls[0]?.[0])
    expect(url).not.toContain('state=')
  })

  it('says the screen is read-only and why', async () => {
    // The promotion ratchet cannot be honoured yet: there is no stored backtest
    // to require and the audit trail cannot record who promoted what.
    stub(200, response())
    renderPage()

    expect(await screen.findByText(/Read-only/)).toBeTruthy()
    expect(screen.getByText(/no stored backtest to require/)).toBeTruthy()
  })

  it('surfaces what the server said when the read fails', async () => {
    stub(503, { detail: 'the database could not be reached' })
    renderPage()

    expect(await screen.findByText(/the database could not be reached/)).toBeTruthy()
  })
})
