import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WorkerConfigPanel from './WorkerConfigPanel'
import type { WorkerConfigScreen, WorkerConfigView } from '@/api/types'

/**
 * The worker settings panel, from the side a person sees.
 *
 * **Three states that must not look alike**, and they are the reason this
 * screen is more than a form. A worker reads its configuration once, at start,
 * so what is saved and what is running are two different facts:
 *
 * 1. a worker is reporting the saved revision — in force;
 * 2. a worker is reporting an older one — saved, not running, and the operator
 *    has to be told to restart or the change simply never happens;
 * 3. nobody is reporting at all — stored and unobserved, which is not the same
 *    sentence as "running nothing".
 *
 * The other half is the live lock. `allow_live_orders` is the third of three
 * live-money locks and it now sits behind a checkbox, so the cases below hold
 * the asymmetry that makes that safe: arming reveals a password box and cannot
 * be submitted without one, and disarming asks for nothing at all.
 *
 * The risk ceilings are the third thing on this screen, and `TestTheRiskLimits`
 * holds the two properties that make editing them safe rather than merely
 * possible: a fraction leaves this form **as typed** — never through `Number`,
 * which is the one conversion that could move a ceiling — and the section is
 * rendered from the server's field list, so a limit the server stops sending
 * stops being posted rather than lingering in a copy kept here.
 */

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function config(overrides: Partial<WorkerConfigView> = {}): WorkerConfigView {
  return {
    symbols: ['SPY'],
    max_silence_seconds: 60,
    strategy: 'sma_crossover',
    strategy_params: {},
    sizing_method: 'risk_pct',
    sizing_value: '0.01',
    stop_type: 'atr',
    stop_multiplier: '2',
    stop_period: 14,
    allow_live_orders: false,
    risk: {
      max_position_pct: '0.10',
      max_gross_exposure_pct: '1.00',
      max_daily_loss_pct: '0.03',
      max_orders_per_minute: 30,
      max_open_positions: 20,
      max_quote_age_seconds: 30,
      default_stop_loss_pct: '0.02',
      default_take_profit_pct: '0.06',
    },
    ...overrides,
  }
}

/**
 * The server's risk field catalogue, trimmed to one of each kind.
 *
 * Two entries rather than eight because the branch under test is `unit`: a
 * fraction is sent as typed and bounded by `maximum`, a count goes through
 * `Number` and has no upper bound. Eight would exercise the same two paths four
 * times each and hide which one a failure came from.
 */
const RISK_FIELDS = [
  {
    name: 'max_position_pct',
    label: 'Max position',
    unit: 'fraction',
    help: 'The most of the account one symbol may become.',
    maximum: '1',
  },
  {
    name: 'max_open_positions',
    label: 'Max open positions',
    unit: 'positions',
    help: 'More than one person can watch is its own risk.',
    maximum: null,
  },
]

function screenPayload(overrides: Partial<WorkerConfigScreen> = {}): WorkerConfigScreen {
  return {
    saved: {
      config: config(),
      revision: 3,
      updated_at: '2026-09-01T14:30:00Z',
      updated_by: 'josh',
    },
    running: null,
    pending_restart: false,
    options: {
      strategies: [
        {
          value: 'sma_crossover',
          label: 'sma_crossover',
          help: 'A moving-average crossover.',
          params_schema: {},
          default_params: { fast_period: 20 },
        },
      ],
      sizing_methods: [
        { value: 'risk_pct', label: 'Risk % of equity', help: 'Size against the stop.' },
        { value: 'fixed_qty', label: 'Fixed quantity', help: 'A share count.' },
      ],
      stop_types: [
        { value: 'atr', label: 'ATR multiple', help: 'Volatility-adaptive.' },
        { value: 'fixed_pct', label: 'Fixed %', help: 'A fraction below entry.' },
      ],
      multiplier_stops: ['atr', 'chandelier'],
      period_stops: ['atr', 'chandelier', 'time'],
      risk_fields: RISK_FIELDS,
    },
    run_mode: 'live',
    allow_live_trading: true,
    live_orders_require_password: true,
    ...overrides,
  }
}

/** One route, two methods: the GET seeds the form and the PUT is the save. */
function stub(payload: WorkerConfigScreen = screenPayload()) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
    return {
      ok: true,
      status: 200,
      statusText: 'stub',
      json: async () => payload,
      // Recorded so a case can assert on what the form actually sent.
      __body: init?.body,
    } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkerConfigPanel />
    </QueryClientProvider>,
  )
}

/** The body of the last PUT, parsed. */
function lastPut(fetchMock: ReturnType<typeof stub>): Record<string, unknown> {
  const put = fetchMock.mock.calls.filter((call) => call[1]?.method === 'PUT').at(-1)
  expect(put).toBeTruthy()
  return JSON.parse(String(put?.[1]?.body))
}

describe('saved versus running', () => {
  it('says nobody has reported when no worker has published', async () => {
    stub()
    renderPanel()

    expect(await screen.findByText(/No worker has reported/)).toBeTruthy()
  })

  it('asks for a restart when the running revision is behind', async () => {
    stub(
      screenPayload({
        pending_restart: true,
        running: {
          config: config({ strategy: '' }),
          revision: 2,
          started_at: '2026-09-01T09:00:00Z',
          trading: false,
          reason: 'no strategy is configured',
        },
      }),
    )
    renderPanel()

    expect(await screen.findByText(/Saved, not running/)).toBeTruthy()
    // The worker's own sentence, not one the screen re-derived.
    expect(screen.getByText(/no strategy is configured/)).toBeTruthy()
  })

  it('says it is in force when the revisions agree', async () => {
    stub(
      screenPayload({
        running: {
          config: config(),
          revision: 3,
          started_at: '2026-09-01T09:00:00Z',
          trading: true,
          reason: 'trading sma_crossover with REAL MONEY',
        },
      }),
    )
    renderPanel()

    expect(await screen.findByText(/In force/)).toBeTruthy()
  })
})

describe('the form', () => {
  it('seeds itself from the saved configuration', async () => {
    stub()
    renderPanel()

    const symbols = (await screen.findByLabelText('Watchlist')) as HTMLInputElement
    expect(symbols.value).toBe('SPY')
  })

  it('relabels the stop input when the type stops being a multiple', async () => {
    stub()
    renderPanel()

    expect(await screen.findByLabelText('Stop multiple')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Protective stop'), { target: { value: 'fixed_pct' } })
    // The same field means a fraction now. An input still labelled "multiple"
    // is how somebody types 2 and gets a stop 200% below entry.
    expect(screen.getByLabelText('Stop distance')).toBeTruthy()
  })

  it('sends the decimals as the strings that were typed', async () => {
    const fetchMock = stub()
    renderPanel()

    const value = (await screen.findByLabelText('Sizing value')) as HTMLInputElement
    fireEvent.change(value, { target: { value: '0.005' } })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    await waitFor(() => expect(lastPut(fetchMock).sizing_value).toBe('0.005'))
  })

  it('refuses malformed strategy parameters without sending anything', async () => {
    // Parsed here as well as on the server. The server still refuses it — this
    // saves a round trip and can name the field.
    const fetchMock = stub()
    renderPanel()

    const params = await screen.findByLabelText('Strategy parameters (JSON)')
    fireEvent.change(params, { target: { value: '{fast: 20}' } })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === 'PUT')).toHaveLength(0)
  })

  it('splits and uppercases the watchlist', async () => {
    const fetchMock = stub()
    renderPanel()

    const symbols = await screen.findByLabelText('Watchlist')
    fireEvent.change(symbols, { target: { value: ' spy, qqq ' } })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    await waitFor(() => expect(lastPut(fetchMock).symbols).toEqual(['SPY', 'QQQ']))
  })
})

describe('the live-orders lock', () => {
  it('cannot be armed without a password', async () => {
    stub()
    renderPanel()

    const checkbox = await screen.findByLabelText(/Permit this worker to place live orders/)
    fireEvent.click(checkbox)

    const save = screen.getByRole('button', { name: /Save configuration/ }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    expect(screen.getByLabelText(/account password/i)).toBeTruthy()
  })

  it('sends the password once one is typed', async () => {
    const fetchMock = stub()
    renderPanel()

    fireEvent.click(await screen.findByLabelText(/Permit this worker to place live orders/))
    fireEvent.change(screen.getByLabelText(/account password/i), { target: { value: 'hunter2' } })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    await waitFor(() => expect(lastPut(fetchMock).password).toBe('hunter2'))
  })

  it('asks for nothing to turn it off', async () => {
    // The same asymmetry the halt and resume buttons have: stopping must never
    // be the harder direction.
    const fetchMock = stub(
      screenPayload({
        saved: {
          config: config({ allow_live_orders: true }),
          revision: 3,
          updated_at: '2026-09-01T14:30:00Z',
          updated_by: 'josh',
        },
      }),
    )
    renderPanel()

    fireEvent.click(await screen.findByLabelText(/Permit this worker to place live orders/))
    const save = screen.getByRole('button', { name: /Save configuration/ }) as HTMLButtonElement
    expect(save.disabled).toBe(false)
    expect(screen.queryByLabelText(/account password/i)).toBeNull()

    fireEvent.click(save)
    await waitFor(() => expect(lastPut(fetchMock).allow_live_orders).toBe(false))
  })

  it('explains that a paper platform ignores it', async () => {
    stub(screenPayload({ run_mode: 'paper' }))
    renderPanel()

    expect(await screen.findByText(/paper mode, which ignores this setting/)).toBeTruthy()
  })
})

describe('the risk limits', () => {
  it('seeds each box from the saved ceilings', async () => {
    stub()
    renderPanel()

    const position = (await screen.findByLabelText(/Max position/)) as HTMLInputElement
    const open = screen.getByLabelText(/Max open positions/) as HTMLInputElement

    expect(position.value).toBe('0.10')
    // A count arrives as a JSON number and still has to render in a text box.
    expect(open.value).toBe('20')
  })

  it('sends a fraction exactly as typed, never as a float', async () => {
    // The property this whole section turns on. `Number('0.07')` is 0.07 today
    // and the ceiling it produces is not reliably the one that was typed —
    // these are multiplied by equity to decide what an order is refused
    // against (CLAUDE.md §1.1). The assertion is on the *string* for that
    // reason: `toBe('0.07')` fails for a value that went through Number, where
    // a numeric comparison would pass.
    const fetchMock = stub()
    renderPanel()

    fireEvent.change(await screen.findByLabelText(/Max position/), { target: { value: '0.07' } })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    await waitFor(() => {
      const risk = lastPut(fetchMock).risk as Record<string, unknown>
      expect(risk.max_position_pct).toBe('0.07')
    })
  })

  it('sends a count as a number', async () => {
    const fetchMock = stub()
    renderPanel()

    fireEvent.change(await screen.findByLabelText(/Max open positions/), {
      target: { value: '12' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))

    await waitFor(() => {
      const risk = lastPut(fetchMock).risk as Record<string, unknown>
      expect(risk.max_open_positions).toBe(12)
    })
  })

  it('bounds a fraction by the server ceiling and leaves a count unbounded', async () => {
    // The browser refusing before the round trip, from the server's own number
    // rather than one copied here. A count has no `maximum`, and inventing one
    // would refuse a value the server accepts.
    stub()
    renderPanel()

    const position = (await screen.findByLabelText(/Max position/)) as HTMLInputElement
    const open = screen.getByLabelText(/Max open positions/) as HTMLInputElement

    expect(position.getAttribute('max')).toBe('1')
    expect(open.hasAttribute('max')).toBe(false)
    expect(open.getAttribute('min')).toBe('1')
  })

  it('renders only the fields the server declares', async () => {
    // The catalogue is the server's, so a ceiling it stops sending stops being
    // posted — rather than lingering in a list kept here and being saved as a
    // field the endpoint no longer has.
    const fetchMock = stub()
    renderPanel()

    await screen.findByLabelText(/Max position/)
    expect(screen.queryByLabelText(/Max daily loss/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Save configuration/ }))
    await waitFor(() => {
      const risk = lastPut(fetchMock).risk as Record<string, unknown>
      expect(Object.keys(risk).sort()).toEqual(['max_open_positions', 'max_position_pct'])
    })
  })

  it('puts the server sentence beside the box it explains', async () => {
    // The prose comes down the wire so that the argument for a number lives
    // with the number rather than in a copy that goes stale.
    stub()
    renderPanel()

    const position = (await screen.findByLabelText(/Max position/)) as HTMLInputElement
    const hint = document.getElementById(position.getAttribute('aria-describedby') ?? '')
    expect(hint?.textContent).toBe('The most of the account one symbol may become.')
  })

  it('does not ask for a password to tighten one', async () => {
    // These bound orders that are already permitted, where `allow_live_orders`
    // grants a new capability — and tightening a ceiling must never be the
    // harder direction.
    const fetchMock = stub()
    renderPanel()

    fireEvent.change(await screen.findByLabelText(/Max position/), { target: { value: '0.05' } })
    const save = screen.getByRole('button', { name: /Save configuration/ }) as HTMLButtonElement
    expect(save.disabled).toBe(false)
    expect(screen.queryByLabelText(/account password/i)).toBeNull()

    fireEvent.click(save)
    await waitFor(() => expect(lastPut(fetchMock).password).toBeUndefined())
  })
})
