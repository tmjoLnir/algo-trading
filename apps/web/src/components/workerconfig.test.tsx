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
    ...overrides,
  }
}

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
